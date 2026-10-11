# ADR-0031: Optional owned training-job monitoring

## Status

Accepted.

## Context

GPU process observations do not describe a provider training job's queue,
replicas or logs. Operators need those observations beside the existing fleet
without sending a personal platform credential to the browser. Zenith's
personal API supplies owner-scoped job lists and diagnostic GETs. A replica is
not a GPU, and provider-native resource values do not declare a percentage scale.

## Candidates

| Approach | Benefit | Cost |
| --- | --- | --- |
| Browser calls the platform directly | No monitor worker | Exposes a platform credential, requires cross-origin access and one request stream per viewer |
| Optional monitor-owned collector and constrained read API | One bounded request budget, private credential, survives closed viewers | Adds a worker lifecycle and explicit stale-state semantics |

## Decision

Use an optional server collector. The normal configuration disables it and
performs no platform requests. Local configuration declares the platform origin,
private token file and polling interval. HTTPS is the default transport policy;
HTTP requires an explicit local opt-in. Enabling the collector requires the
monitor's Bearer authentication, because job logs are private even on an
otherwise authentication-free deployment.

`training_config` owns validation; `training_client` owns bounded GETs and
credential handling; `training_projection` owns the fixed response projection;
`training_jobs` owns the cached observations and worker. `web_training` exposes
the `TrainingSource` protocol through manifest-declared reads. The entrypoint
starts and stops the worker. Tests inject sources and transport responses rather
than contacting a real training platform. No runtime provider registry is needed.

Lists are paginated and deduplicated, with an explicit completeness flag and a
bounded automatic diagnostic set. Network failure retains the previous sample
with a stale flag and retry delay. Authentication failure clears private caches.
Details revalidate task ownership before returning cached logs. Request, response,
sample, replica, log and cache bounds apply independently. Redirects never carry
the credential to another endpoint. No platform write is supported.

## Consequences

New tasks appear on the next successful collection; closing the page does not
stop collection. The dashboard separates platform status observation time from
metric sample time. Missing values remain unknown, actual zeros remain zero,
and native units are not converted without an authoritative unit contract.
Logs are bounded samples, not a complete history or a terminal-state signal.

The integration is installed from tracked source and requires no installation
patch or private address in the repository. Existing SSH collection, inventory,
GPU counts, presentation preferences and read/write authentication retain their
contracts. A future provider contract change can be tested at the client and
projection boundary without changing HTTP dispatch.
