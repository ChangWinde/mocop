# ADR-0015: Attended-aware process cadence and the identity workload tier

## Status

Accepted

## Context

Process telemetry exists for human readers: the GPU dialog, the owners view, and
the process event timeline. The monitored production fleet keeps the dashboard
closed most of the day, yet busy hosts still ran the measured-slow compute-process
query every fifteen seconds around the clock. At the same time the dialog could
not answer the two questions operators actually ask about a process — "how long
has it been running?" and "who is running what?" — unless the full workload mode
was enabled, whose per-PID cgroup and environment reads are unnecessary on hosts
without Slurm or Kubernetes.

## Driving factors

- Reduce steady-state NVIDIA command count when nobody is watching, without
  touching core telemetry, trends, or incident cadence.
- Provide per-process runtime and ownership as essential, well-usable features at
  the smallest defensible remote cost.
- Keep the fixed-command agentless SSH boundary, the bounded parser, and the rule
  that no browser request triggers a remote query.

## Decision

Three cooperating changes:

- **Viewer presence.** Any dashboard-marked API read and every wake of a connected
  event stream refresh a monotonic presence timestamp; half a minute of silence
  means unattended. The scheduler relays this to the probe, which stretches the
  process cadence of every device — busy or idle — to sixteen times the base
  interval while unattended. The first returning viewer forces a catch-up process
  sample on the next core cycle. Attended behavior, including the idle stretch and
  its activity-hint cancellation, is unchanged.
- **Identity workload tier.** `workloads.mode: "identity"` reads only
  `/proc/PID/status` (real UID, resolved through the root-owned passwd database),
  `/proc/PID/stat` (true start time via btime and clock ticks), and a bounded 255
  byte `/proc/PID/cmdline`. It never reads cgroup or environment data and never
  classifies schedulers; `"auto"` layers those on top. Protocol `MONITOR_V7`
  appends the start epoch and command line to workload records while `V6` payloads
  remain parseable.
- **Monitor-relative first-seen.** The service already tracks process transitions,
  so each active process carries a first-seen timestamp at zero remote cost. The
  dialog shows it as an observed runtime lower bound whenever the true start time
  is unavailable, and it resets honestly on service restart.

## Impact

- An unwatched busy host drops from 16 to about 12.25 NVIDIA commands per minute
  at the default cadences; unwatched idle hosts keep their existing stretch.
- Unattended process-event granularity coarsens to the stretched interval; the
  event timeline records fewer short-lived processes while nobody is watching.
  Incident recovery for process-derived domains can defer by at most the stretched
  interval.
- The dialog answers runtime and ownership with `identity` at roughly a third of
  the full tier's per-PID reads, and still shows the observed runtime lower bound
  with workloads disabled.
- Presence marking adds one atomic timestamp write per dashboard read or stream
  wake; no new configuration keys and no browser-triggered remote queries.
