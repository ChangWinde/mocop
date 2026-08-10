# Performance

Mocop spends most collection time waiting for SSH connection and network I/O. Python is appropriate for the current workload because changing the implementation language would not remove a remote round trip or shorten a connection timeout.

This document defines reproducible measurement conditions and architecture thresholds. It contains no production inventory or telemetry.

## Hot-path design

- One bounded transport process collects system metrics, GPU metrics, compute tasks, and optional GPU health for one host per cycle; remote targets use one logical SSH session and the optional local target bypasses SSH.
- The optional hardware-health section uses one additional short `nvidia-smi` query inside that transport. Its failure is isolated and does not cause a retry or invalidate base telemetry.
- `max_workers` bounds concurrent probes, while completed hosts publish independently.
- A validated per-host override can pace a measured slow target and extend only its complete-probe timeout; it should not be used without repeated timing evidence.
- Repeated failures back off to at most 60 seconds instead of occupying a connection slot every cycle.
- Stdout and stderr are drained incrementally under one byte limit; timeout and overflow terminate the process group.
- Snapshots, trends, and incidents use bounded memory structures with no database write path.
- SSE publishes each completed host result and one authoritative cycle completion; it does not duplicate the full snapshot when a cycle merely starts. The browser coalesces same-frame work with `requestAnimationFrame`.
- GPU groups, host lists, heatmap cells, and incident panels reuse DOM when their input signature is unchanged; the browser consumes backend incident decisions instead of re-evaluating thresholds.
- GPU groups start collapsed, which bounds initial table rendering in the cluster-wide view.
- Maintenance evaluation is an in-memory pass over the configured host-window map during snapshot publication; it starts no timer, process, probe, or database write.
- Capacity matching scans the existing browser snapshot only while its dialog is open; it starts no request and groups devices by host and model in linear time before sorting the bounded candidate set.
- Host-group metadata adds one constant-time lookup per server snapshot; grouped fleet headers and host rows reuse cached DOM signatures across SSE updates.

## OpenSSH connection reuse

Mocop always delegates connection behavior to the selected OpenSSH configuration. It does not maintain a second connection pool. Inspect an alias with:

```bash
ssh -G gpu-node-01 | grep -E '^(controlmaster|controlpath|controlpersist) '
```

If `ControlMaster` is enabled, its control directory must be accessible only to the operator. Mocop does not change that policy.

## Reproducible checks

The browser fixture uses three fictional nodes and eight GPUs:

```bash
node --experimental-websocket tests/browser_smoke.mjs
```

This test covers collapsed GPU groups, GPU task and health details, capacity matching, shared node grouping, drag ordering, display preferences, the scheduling heatmap, resource cards, authoritative incidents, transient SSE errors, responsive layout, and the runtime-cadence race. CI duration is not a performance benchmark.

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
