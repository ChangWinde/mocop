# ADR-0008: Configured SSH connection topology

## Status

Accepted

## Context

Mocop connects through OpenSSH aliases, but the dashboard currently shows node health
without explaining how the monitoring host reaches a node. This is especially opaque
when an alias terminates at a loopback FRP visitor and other nodes use that machine as
an SSH jump host. Operators need a small, durable connection map for diagnosis without
turning Mocop into an FRP controller or exposing authentication material.

## Driving factors

- Keep OpenSSH configuration authoritative for command execution.
- Keep the monitored-host allowlist as the only authority for resource collection.
- Represent the operator's logical route, including FRP hops, without credentials,
  usernames, addresses, arbitrary commands, or automatic network discovery.
- Add no SSH round trip and no repeated static data to the telemetry SSE stream.
- Fail closed on malformed, cyclic, disconnected, or ambiguous configured links.
- Let a dashboard inventory addition remain possible before its route is documented.

## Candidates

### Option A: Resolve every alias with `ssh -G` and infer the graph at runtime

Pros: avoids duplicating `ProxyJump` metadata and follows OpenSSH changes automatically.

Cons: depends on environment-sensitive OpenSSH expansion, does not explain the FRP
provider/visitor relationship, can expose network identities, and makes a display
request execute another local process.

### Option B: Store a full operational network model

Pros: could include FRP servers, proxy names, listener ports, failover routes, service
units, and enough detail to automate repair.

Cons: duplicates sensitive configuration, creates a second control plane, expands the
browser's authority, and is disproportionate to a monitoring dashboard.

### Option C: Store a bounded logical connection tree in Mocop configuration

Pros: deterministic, reviewable, dependency-free, expressive enough for direct SSH and
FRP-backed hops, safe to render as text, and independent of telemetry collection.

Cons: operators must update the map when the SSH route changes; a newly added host is
shown as unmapped until its link is documented.

Three node-identity variants were considered within this option. Reusing `hosts` for
every endpoint is compact but would make display-only gateways become probe targets.
Adding a second structured node registry could encode roles explicitly, but would
duplicate aliases and create another synchronization surface. Accepting bounded safe
aliases in the tree and joining them to the live snapshot is both smaller and safer:
an alias has telemetry only when it independently belongs to the active inventory.

## Decision

Choose Option C with snapshot-based node identity. The optional `topology` object
contains one safe display-only `root` and a bounded list of directed links. Each link
contains only `source`, `target`, an enumerated `transport` (`ssh`, `frp-stcp`,
`frp-xtcp`, or `vpn`), and an optional short visible `label`. A target has at most one
parent, every configured link must be reachable from the root, and cycles, duplicate
links, self-links, unsafe aliases, control characters, and unknown fields are rejected
at startup.

Topology aliases are not collection authority. They may identify the monitoring
machine, a jump host, an FRP endpoint, or a monitored server, and may also appear in
`exclude_hosts`. Only aliases independently admitted by the active host inventory are
passed to `ResourceProbe`. The browser joins topology aliases to the current snapshot:
matched aliases receive live resource status; unmatched aliases are neutral
infrastructure nodes and never appear offline merely because they are not probed.

The tree may omit a monitored host so dashboard-based inventory additions remain
possible. Such hosts are visibly separated as unmapped. Removing an inventory host
also removes links that reference it; removing the root clears the topology. The
topology is descriptive only and never supplies an SSH destination or process argument.

`GET /api/topology` returns the validated projection without scanning OpenSSH aliases
or starting a process. The browser fetches it at startup and after dashboard inventory
changes, then combines it with the existing live snapshot to render node status. Static
topology is deliberately excluded from SSE and OpenMetrics payloads.

## Impact

- The operator configuration becomes the durable, machine-readable connection diagram.
- FRP-backed routes are visible without storing FRP credentials or controlling FRP.
- Jump hosts and monitoring infrastructure remain visible without becoming probe targets.
- Existing configurations remain compatible because `topology` is optional.
- Inventory removal preserves a valid configuration and may leave descendants unmapped.
- Route accuracy remains an operator-owned assertion; Mocop displays live reachability
  but does not claim that the configured logical hop caused a connection result.
