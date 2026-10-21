# Pi log review — 2026-10-09 America/New_York

Reviewed `recordtwitch`, its three most recently modified recorder logs, the
current boot journal, live recorder telemetry and system health on
`raspberrypi.local`. Pi log timestamps use Europe/London (BST); this review ran
after midnight on October 10 in that timezone.

The host is a Raspberry Pi Zero W Rev 1.1. The recorder and EventSub were running,
both configured channels were offline, and background compression was advancing.

| Finding | Evidence and impact | Status / next action |
| --- | --- | --- |
| Streamlink startup check times out | Repeated warnings from a five-second version check. A native check succeeded with `streamlink 5.5.1`, taking 15.11 seconds. | Changed the check to allow 30 seconds in local and deployed source. No recorder restart; the running service continues normally, and the new allowance applies at its next startup. |
| Compression cannot finish within its timeout at the observed speed | The 1,906-second Pyka recording encodes at roughly 0.015–0.021 times real time. That implies about 25–35 hours, versus a duration-based timeout of 5,665 seconds (94 minutes). | Still needs a compression policy decision. Prefer doing compression on a faster host. If keeping it on this Pi, benchmark a faster preset and introduce an encoding-specific timeout, progress watchdog and retry backoff. Simply increasing the timeout leaves very slow work occupying the Pi. |
| Repeated old compression errors | The three reviewed logs contain 1,227 `Idle compress failed` entries. Sampled FFmpeg errors reject an output ending in `.mp4.compressing` because no output container can be inferred. | The prior dashboard/processing deployment already changed temporary outputs to `.tmp.mp4`. The current encoder advances normally. Historical messages remain in the logs. |
| Recording failures lack diagnostic detail | Three missing-file errors for `IMPROMTU EMU NIGHT` on October 7. The deployed recorder discards Streamlink stdout and stderr, so the reason is unavailable. Its monitor can repeat missing-file warnings while the child remains alive. | Add bounded Streamlink error capture, log exit status, and terminate/recover recordings that produce no data within a configurable startup/stall allowance. The old logs cannot establish why that recording failed. |
| Temporary DNS outages | Both channel API checks failed to resolve `api.twitch.tv` on October 6 and October 9. | Historical; current subscriptions and channel checks succeed. Monitor recurrence before changing network configuration. |
| Failed boot service | `systemd-modules-load.service` is failed. Boot logs show `g_mass_storage` failing with `no file given for LUN0`. `/etc/modules` loads that module, and `/boot/config.txt` enables `dwc2`. | Determine whether USB mass-storage gadget mode is intended. Configure a backing file if needed, or remove the unused module request. No boot configuration was changed during this review. |
| EventSub keepalive disconnects | Historical `keepalive expired` warnings; live health reports zero failures since the preceding fix/restart. | Five-second arrival grace is deployed. Fallback polling remains 400 seconds during outages. |

At the time of review, the root filesystem was 46% used with approximately 61 GiB
available. RAM had about 240 MiB available; swap usage was 17 MiB. Temperature was
48.2°C, and `vcgencmd get_throttled` returned `0x0`. The reviewed boot journal
showed no OOM kills, storage I/O errors, or explicit Wi-Fi disconnect/crash events.

Other boot warnings include optional Wi-Fi board configuration falling back to
available firmware, Bluetooth SAP/privacy initialization, an obsolete `elevator`
parameter, and no IPv6 router. They do not explain a current recorder failure;
IPv4 Twitch connectivity is working. The system clock is synchronized.

Priority: move or redesign compression first, then improve recording failure
diagnostics and stall recovery. USB gadget cleanup is separate from the recorder.
Recording quality, compression settings, polling interval and boot configuration
were unchanged in this review.

Follow-up on October 10: compression now suspends its existing encoder during
recordings, remuxes new footage before resuming, and uses a 30-minute progress
watchdog instead of the 94-minute total runtime limit. It can therefore use the
Pi's idle days without discarding progress each time recording starts. See
`MONITORING.md` for behavior and the service-restart limitation.
