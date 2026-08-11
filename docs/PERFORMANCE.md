# Performance

Mocop spends most collection time waiting for SSH connection and network I/O. Python is appropriate for the current workload because changing the implementation language would not remove a remote round trip or shorten a connection timeout.

This document defines reproducible measurement conditions and architecture thresholds. It contains no production inventory or telemetry.

## Hot-path design

- One bounded transport process collects system metrics, GPU metrics, compute tasks, and optional GPU health for one host per cycle; remote targets use one logical SSH session and the optional local target bypasses SSH.
- Base GPU and hardware-health fields share one `nvidia-smi` query. Compute tasks use a second query. If combined health fields are unsupported, collection falls back to base GPU fields without retrying the SSH connection or hiding system telemetry.
- Core GPU and system data retain the host cadence. The independent process cadence defaults to 15 seconds, so three five-second core samples execute four NVIDIA commands instead of six. Skipped task cycles reuse a timestamped last-good sample without creating process transitions.
- One persistent pool avoids per-cycle executor churn. `max_workers` bounds active
  probes, while a scheduler-owned deadline independently paces each host and prevents
  self-overlap. Oldest due deadlines run first, and an event wakes the scheduler only
  for an earlier deadline, completed probe, inventory change, or shutdown.
- A validated per-host override can pace a measured slow target and extend only its complete-probe timeout; it should not be used without repeated timing evidence.
- Repeated failures back off to at most 60 seconds; bounded per-host jitter prevents synchronized retries after a shared path recovers.
- Stdout and stderr are drained incrementally under one byte limit; timeout and overflow terminate the process group.
- The fixed Linux sample combines CPU, memory, load, uptime, network, and block-I/O
  reads in one `awk` pass. On the default no-workload path this reduces external
  utility invocations from 14 to 6 per host sample. Active process groups also accept
  a lifecycle cancellation signal, so a service restart does not wait for the probe
  timeout.
- Snapshots use explicit serializers that allocate each response container once; they
  do not recursively convert immutable models and then deep-copy the complete result.
  Trends and incidents use bounded memory structures. In-memory host/GPU trends use
  compact immutable records. Optional SQLite persistence receives non-blocking inserts
  through a bounded queue and batches writes on one dedicated thread; when persistence
  is disabled, collection performs no persistence serialization or write call.
- Optional workload identity adds bounded `/proc` reads only for active GPU PIDs and is
  disabled by default. It never calls a scheduler API.
- SSE publishes each completed host result and the latest completed submission-batch
  timing; it does not duplicate the full snapshot when a probe merely starts.
  Concurrent SSE readers share one read-only projection per observable state revision,
  and the HTTP server serializes that revision once. Public snapshot reads receive a
  deep copy, so client mutation cannot alter cached state. The browser coalesces
  same-frame work with `requestAnimationFrame`.
- GPU groups, host lists, heatmap cells, and incident panels reuse DOM when their input signature is unchanged; the browser consumes backend incident decisions instead of re-evaluating thresholds.
- GPU groups start collapsed, which bounds initial table rendering in the cluster-wide view.
- Maintenance evaluation is an in-memory pass over the configured host-window map during snapshot publication; it starts no timer, process, probe, or database write.
- Capacity matching scans the existing browser snapshot only while its dialog is open; it starts no request and groups devices by host and model in linear time before sorting the bounded candidate set.
- Host-group metadata adds one constant-time lookup per server snapshot; grouped fleet headers and host rows reuse cached DOM signatures across SSE updates.
- The connection map builds its bounded static tree only when topology changes; live snapshots update existing node state and unmapped-host controls in place.
- Topology correlation is a bounded in-memory tree pass only when incidents are read or
  a transition is sent. Webhook targets use independent background queues and cannot
  block collection.
- GPU history adds one bounded in-memory append per device and one non-blocking
  persistence batch per completed host probe. Manual requests enter the existing host
  scheduler and cannot create a second pool or overlap the same host.
- `/metrics` performs one bounded snapshot copy and a linear serialization pass in the HTTP request thread; it starts no probe, worker, timer, database query, or write.

## OpenSSH connection reuse

Mocop always delegates connection behavior to the selected OpenSSH configuration. It does not maintain a second connection pool. Inspect an alias with:

```bash
ssh -G gpu-node-01 | grep -E '^(controlmaster|controlpath|controlpersist) '
```

If `ControlMaster` is enabled, its control directory must be accessible only to the operator. Mocop does not change that policy.

## Reproducible checks

The scheduler fault-injection test holds one host probe open and requires a peer to
complete three independent submissions without overlapping the blocked host:

```bash
python3 -m unittest \
  tests.test_service.MonitorServiceTests.test_run_schedules_each_host_without_waiting_for_a_slow_peer
```

This proves the scheduling invariant; it is not a throughput benchmark.

The browser fixture uses three fictional nodes and eight GPUs:

```bash
node --experimental-websocket tests/browser_smoke.mjs
```

This test covers collapsed GPU groups, GPU task and health details, capacity matching, shared node grouping, drag ordering, display preferences, the scheduling heatmap, resource cards, authoritative incidents, transient SSE errors, responsive layout, and the runtime-cadence race. CI duration is not a performance benchmark.

To compare cached status updates with a forced rebuild of a synthetic 513-node
connection tree in the same browser process, run:

```bash
MOCOP_TOPOLOGY_BENCHMARK=1 node --experimental-websocket tests/browser_smoke.mjs
```

The output reports 20 warmed-up samples with median, P95, and maximum durations. It is
diagnostic evidence rather than a timing gate because runner hardware is not stable.

Reference measurement on 2026-08-10 used Chrome 149 on an AMD Ryzen 9 9950X with a
513-node synthetic tree, three warm-up pairs, and 20 measured pairs. A forced rebuild
measured 28.0 ms median, 33.9 ms P95, 39.5 ms maximum, and 3.64 ms standard deviation.
The cached unchanged-snapshot path measured 0.2 ms median, 0.4 ms P95, 0.5 ms maximum,
and 0.12 ms standard deviation. These numbers document the optimization on one machine;
the command above is the reproducible contract.

The same host, with one NVIDIA GeForce RTX 4090 on driver 580.173.02, measured 20
warmed, interleaved local fixed-script samples before and after combining base GPU and
health fields into one `nvidia-smi` query. Both paths included payload parsing. Median
complete collection fell from 40.7 ms to 29.5 ms, P95 from 42.4 ms to 32.2 ms, and
standard deviation from 1.2 ms to 1.1 ms: a 27.5% median reduction (1.38x throughput)
with identical parsed GPU, task, and health data. Remote SSH latency is not included in
this local comparison.

On the same host, five warm-ups followed by 30 local samples compared the separate
POSIX helper pipeline with the combined system pass while keeping both NVIDIA queries
and payload parsing unchanged. External utility invocations fell from 14 to 6 (57.1%).
Median complete-sample time fell from 31.63 ms to 29.75 ms (5.9%), and P95 from
32.25 ms to 31.18 ms. This is intentionally reported as a remote-process overhead
reduction, not a claim that end-to-end SSH latency improved by 50%.

A synthetic snapshot fixture used 200 hosts, 1,600 GPUs, 3,200 GPU processes with
workload metadata, and four disks per host. Five warm-ups preceded 30 samples. Removing
duplicate recursive conversion and the redundant final deep copy reduced median
snapshot time from 81.05 ms to 2.18 ms (97.3%), P95 from 83.91 ms to 3.90 ms, and peak
transient allocation from 9,278,691 to 4,270,259 bytes (54.0%). The returned response
remains deeply isolated from store state, as enforced by a mutation regression test.

On 2026-08-11, Python 3.10.12 on the same x86-64 development host measured JSON
delivery for a 200-host, 1,600-GPU snapshot (1,231,602 bytes). Five warm-ups preceded
30 cold serializations: median 5.225 ms with 0.140 ms standard deviation. After adding
the per-revision response cache, 1,000 repeated lookups of the already serialized
revision measured 0.000952 ms median with 0.000240 ms standard deviation. This removes
redundant serialization when several API/SSE clients consume one revision; it does not
make the first serialization or remote collection faster. The uncached snapshot model
build remained a separate 3.105 ms median measurement over 30 samples.

A 20-sample lifecycle fixture started a five-second local child, allowed 20 ms for
startup, and then cancelled the owning probe. Direct active-process interruption
reduced cancellation latency from 231.77 ms median to 0.129 ms (99.94%); post-change
P95 was 0.267 ms and every sample retained the explicit cancellation result. Real
service recovery also includes systemd's configured restart delay.

Three warm-ups followed by 15 real local samples on the same host confirmed that the
new process lifecycle tracking did not regress collection: median remained 30 ms,
with 31 ms P95 and a 29--31 ms range.

On 2026-08-11, a Python 3.10.12 synthetic retention workload applied 720 successful
samples for 50 hosts with eight GPUs each: 36,000 host points and 288,000 GPU points.
The captured pre-change retained allocation was 127.363 MiB and the run completed in
2.732 seconds. Three post-change runs retained 37.836--37.856 MiB (37.856 MiB median)
and completed in 1.668--1.731 seconds (1.701 seconds median). This is a 70.3% retained
allocation reduction and a 37.7% wall-time reduction for the fixed in-process state
workload; it does not measure SSH or browser latency. A separate five-run `cProfile`
fixture applying 12,000 samples across 100 hosts and eight GPUs measured 0.606 seconds
median after the change, compared with the captured 1.089-second pre-change profile.
The before-edit measurements were each captured once, so these figures are diagnostic
evidence rather than a release timing gate.

A same-day follow-up used independently allocated metric values to model fresh parser
output for 20 hosts, eight GPUs per host, and 300 retained samples. Two warm-ups
preceded five measurements. Packed numeric trend values, allocation-free existing-key
lookups, and disabled-persistence fast paths reduced median apply time from 0.487610
seconds (0.005581 standard deviation) to 0.357120 seconds (0.004838 standard
deviation), a 26.8% reduction. Retained allocation fell from 14.498 MiB to 7.726 MiB,
a 46.7% reduction. Separately, five thousand parses across seven measured runs reduced
the median eight-GPU combined CSV parse from 0.0596 ms to 0.0434 ms (27.2%). A
132-active-condition snapshot fixture, measured in seven runs of 3,000 snapshots,
fell from 0.2572 ms to 0.1640 ms median (36.2%) after reusing maintenance and action
maps once per snapshot. These in-process results do not include SSH latency.

A loopback HTTP/1.1 fixture then issued 200 snapshot requests over one reused
connection. Separately writing headers and JSON exposed a delayed-ACK stall: median,
P95, and maximum response time were 40.999, 41.997, and 42.090 ms. Enabling
`TCP_NODELAY` on accepted dashboard sockets reduced them to 0.132, 0.150, and 0.470
ms, respectively. New-connection behavior remained unchanged at 0.257 ms median.

A final same-day pass measured four independent in-process hot paths. With 1,000
configured hosts, 50,000 per-host override lookups fell from 292.192 ms to 5.343 ms
median after building immutable indexes at config construction (98.2%). Seven runs of
5,000 eight-GPU combined CSV parses fell from 0.042969 ms to 0.040099 ms per parse
(6.7%) after normalizing optional numeric fields once. Seven asynchronous SQLite runs,
each inserting 8,000 GPU points and 1,000 process events, fell from 105.184 ms to
63.804 ms median (39.3%) after batching rows per telemetry item. Finally, selecting 32
due hosts from a 10,000-host synthetic scheduler fixture fell from 1.127 ms to 0.641 ms
median (43.2%) by retaining only the earliest deadlines instead of sorting every due
host. These fixtures isolate local overhead; they do not measure or predict SSH
network latency.

The tiered-process deployment was then measured against the live 11-node, 47-GPU
inventory at the five-second global cadence. Across six completed core samples for
each of the nine standard-cadence remote nodes, 54 device queries and 18 process
queries replaced the previous 108 NVIDIA queries: 72 commands total, exactly 33.3%
fewer. Including one intentionally 30-second overridden node produced 74 commands
instead of 110, a 32.7% fleet-wide reduction. A separate 20-second observation saw 19
sampled and 32 skipped host results with no cached process-set or observation-time
mismatch. During a passive 30.005-second window, the main process used 0.233% CPU and
31.7 MiB RSS; the complete service cgroup used 1.245% CPU and 48.0 MiB while all 11
nodes and 47 GPUs remained online. These figures describe this inventory and should
not be generalized to other drivers or network paths.

The same live inventory later exposed six concurrent SSE clients. Before sharing the
snapshot projection, three 20-second samples used 0.65%, 0.55%, and 0.55% main-process
CPU (0.55% median). With the projection cache and the same client count, cadence,
worker limit, and online inventory, three post-restart samples each used 0.45% CPU: an
18.2% median reduction. Main-process RSS was 33.0 MiB, with 11/11 nodes, 47 GPUs, and
no collector error. The measurement isolates duplicate server-side snapshot work; it
does not predict CPU use for a different fleet or collection cadence.

Measure one complete collection in an authorized environment with:

```bash
/usr/bin/time -v mocop --once > /tmp/mocop-snapshot.json
```

The output contains inventory and telemetry. Keep it in a controlled location and remove it according to local data-handling policy.

An optimization comparison must hold these inputs constant:

- Mocop commit and configuration
- SSH configuration, connection reuse, and `known_hosts`
- target set, online state, worker count, and timeouts
- global cadence and every per-host cadence/timeout override
- warm-up count, sample count, CPU/RSS collection method, and wall-clock method
- browser version, viewport, GPU count, DOM count, and forced-layout behavior

Report sample count, median, P95, and maximum. A single best result is not evidence of improvement.

## Architecture thresholds

Profile the same workload again before considering a persistent agent, hierarchical collection, or a language rewrite when any of these conditions becomes real:

- more than 200 monitored hosts
- sustained collection below a 2-second interval
- one CPU core remains saturated
- resident memory exceeds 512 MiB
- SSH connection time is no longer the dominant cost

Every optimization must preserve timeouts, output limits, host-key checking, failure isolation, and per-host publication semantics.
