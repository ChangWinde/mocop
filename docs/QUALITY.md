# Quality and resource assessment

This document records the current cross-dimensional assessment of Mocop. It
separates measured evidence from architectural expectations and from work that
has not been measured. Detailed benchmark history remains in
[Performance](PERFORMANCE.md); security claims remain in
[Security](SECURITY.md).

## Assessment summary

| Dimension | Current assessment | Evidence | Residual boundary |
|---|---|---|---|
| Collection performance | Strong at the intended fleet size | A live 11-node, 47-GPU deployment used 0.233% main-process CPU and 31.7 MiB RSS at a five-second cadence; the complete cgroup used 1.245% CPU and 48.0 MiB. Local controllable collection overhead was 0.79 ms of a 31.43 ms sample. | SSH setup, network delay, and `nvidia-smi` dominate and vary by site. |
| State and serialization | Strong, with a clear scale trigger | The reproducible 200-host/1,600-GPU/6,400-process fixture built a 2.78 MiB snapshot. Cold JSON serialization measured 7.15–8.29 ms median; revision-cached JSON and OpenMetrics retrieval measured about 0.002 ms median. | The full snapshot grows linearly with active process records; re-profile before exceeding 200 hosts or if browser transfer becomes visible. |
| Browser responsiveness | Strong for current bounded views | The 65,536-process browser fixture retained only 200 search rows. Two consecutive runs measured 90.4–96.0 ms cold search, 26.9–30.6 ms bounded warm medians, 1.6–1.7 ms cold GPU-summary derivation, and at most 0.1 ms cached summary medians. | Browser timings are diagnostic, not CI thresholds; low-power clients need separate profiling. |
| Memory and retention | Bounded by design and acceptable in measured deployments | Production observation measured 31.7 MiB main-process RSS. The synthetic large fixture attributed 10.20 MiB to `StateStore`; a bounded 20-host/160-GPU retention soak changed traced allocation by 12.8 KiB (0.36%) after stabilization. | The synthetic process RSS of 71.67–71.73 MiB includes the fixture, payloads, tracing, and interpreter, so it is not an idle-service claim. Multi-day live soak remains environment-specific release evidence. |
| Robustness | Strong defense-in-depth | Probe deadlines, output caps, process-group cancellation, per-host isolation, bounded queues, authenticated HTTP, framing/authority checks, and persistence failure isolation have focused unit and integration coverage. | Correct configuration, private capability storage, OpenSSH host verification, and protected transport remain operator responsibilities. |
| Stability | Strong deterministic coverage; long-running evidence is scoped | Unit, contract, browser, cancellation, bounded-retention, restart, migration, and fault-injection suites exercise the major state transitions. Python branch coverage measured 88% and CI rejects a regression below 85%; HTTP thread and file-descriptor counts returned to baseline after the synthetic lifecycle check. | Coverage locates unexecuted paths but does not prove assertion quality, and one local test run does not replace a multi-day soak on the operator's drivers, network, and SSH topology. |

## Resource model

Mocop uses one Python service process, one persistent bounded probe pool, one
thread-per-active HTTP connection, and optional bounded background workers for
persistence and webhooks. Important configured or protocol limits include:

- at most 1,024 monitored aliases, 64 concurrent probes, 256 GPUs per host, and
  4,096 reported GPU processes per host;
- at most 64 concurrent HTTP connections and 16 SSE streams;
- bounded host, GPU, process-event, incident, notification, and SQLite queues or
  rings; and
- a 100-row per-GPU process display and 200-row fleet-search display after the
  full match count is calculated.

The process workspace added in ADR-0020 is a browser-side projection of the
already authenticated snapshot. Its weak-map cache follows immutable GPU object
lifetimes, so it adds neither remote commands nor retained copies of old
snapshots.

## 2026-08-15 reproducible runtime profile

Run the opt-in backend profile with:

```bash
python3 -m tests.benchmarks.runtime_profile
```

Two consecutive reference runs used Python 3.14.6 on Linux x86-64 with 200
hosts, eight GPUs per host, and four processes per GPU. Five warm-ups preceded
measured latency samples. Stable sizes are shown exactly; timing and RSS ranges
show the two observed runs rather than selecting the faster one.

| Measurement | Result |
|---|---:|
| Traced `StateStore` allocation | 10.20 MiB |
| Snapshot JSON size | 2,777,329 bytes |
| OpenMetrics size | 1,843,128 bytes |
| Snapshot view median | 0.0012 ms |
| Isolated deep-copy median | 14.6092–15.3394 ms |
| Cold JSON serialization median | 7.1535–8.2933 ms |
| Cached JSON / metrics median | 0.0021–0.0022 / 0.0020–0.0023 ms |
| Diagnostic gzip level 5 | 53,072–53,077 bytes (1.91%), 3.8652–3.9332 ms median |
| HTTP lifecycle | 1 -> 2 -> 1 threads; 6 -> 6 -> 5 file descriptors |

Compression is reported only to expose a future network trade-off. Mocop does
not currently promise compressed snapshot responses, and the repetitive
synthetic fixture compresses better than real telemetry may.

Run the corresponding browser profile with:

```bash
MOCOP_PROGRAM_SEARCH_BENCHMARK=1 \
  node --experimental-websocket tests/browser_smoke.mjs
```

The benchmark asserts result equivalence in addition to reporting timings. It
also verifies that only 200 result wrappers are retained and that all 65,536
processes contribute to the process-summary count.

## Failure and stability evidence

The verification surface intentionally includes more than happy-path unit tests:

- slow and stuck peers cannot serialize the fleet scheduler;
- cancellation terminates active process groups and closes wake-up descriptors;
- malformed, oversized, ambiguous, or unauthenticated HTTP requests fail before
  route-specific work;
- unavailable or intentionally cached process telemetry remains explicit and
  does not create false process transitions;
- persistence and notification work is admitted through bounded queues and
  shutdown tests cover full queues and delayed workers;
- migrations, restart restoration, retention pruning, corrupt input, and
  disabled optional adapters have regression coverage; and
- the real browser fixture covers authenticated SSE reconnects, responsive
  layout, keyboard focus, bounded process views, filters, drill-down, copy
  actions, and global search transitions.

The 2026-08-16 Python 3.10 run executed 467 tests and measured 88% combined
statement/branch coverage with Coverage.py 7.15.4. CI enforces a conservative
85% floor as a regression signal; the focused contracts and failure-injection
oracles remain the acceptance evidence.

These tests establish deterministic invariants. Operators evaluating a new
driver, tunnel, or unusually large fleet should additionally record at least one
representative soak with CPU, RSS, reconnect count, probe latency, and dropped
queue counters.

## Decisions and next thresholds

No collector rewrite or extra process-utilization query is justified by the
current measurements. The NVIDIA compute-apps source provides allocated VRAM,
not trustworthy per-process SM utilization, so the UI states that limitation
instead of copying device utilization onto a process.

Reconsider a separate paginated process API or compressed transport only after
measurement shows snapshot transfer or browser parsing is the active bottleneck.
Re-profile the complete architecture before any of these conditions is real:

- more than 200 monitored hosts;
- sustained collection below two seconds;
- one CPU core remains saturated;
- resident memory exceeds 512 MiB; or
- SSH connection time is no longer the dominant cost.

Until then, the highest-value operational improvements are connection reuse,
representative live soak evidence, and enabling workload identity only where the
extra bounded `/proc` reads are worth the attribution detail.
