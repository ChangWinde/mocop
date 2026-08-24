# ADR-0022: Resolved SSH topology discovery

## Status

Accepted

## Context

Literal OpenSSH alias discovery cannot distinguish a compute target from a jump
host. An alias named `bastion` is suggestive but naming conventions vary, while
the effective `ProxyJump` or `ProxyCommand` route is stronger evidence. Requiring
operators to duplicate that route as Mocop topology and host groups makes normal
SSH inventory changes unnecessarily repetitive.

ADR-0008 deliberately selected an operator-authored display tree because runtime
resolution could be environment-sensitive and execute a local process. That
trade-off remains appropriate for existing installations, but it leaves no
opt-in path for operators who prefer OpenSSH to remain the topology authority.

## Driving factors

- Keep existing configurations and explicit inventory decisions compatible.
- Use the effective OpenSSH policy rather than jump-host naming heuristics.
- Do not open a network connection while discovering topology.
- Bound subprocess count, duration, output, refresh rate, and concurrency.
- Do not expose resolved usernames, addresses, or raw proxy commands.
- Keep explicit hosts, exclusions, groups, and configured topology authoritative.
- Fail closed rather than probing an unresolved automatically discovered alias.

## Candidates

### Option A: Change alias auto-discovery globally

Treat every `auto_discover` scan as topology-aware and remove inferred proxy
aliases from the monitored inventory.

Pros: no new configuration and immediate convenience.

Cons: an upgrade can silently change the monitored inventory and grouping of an
existing installation; repeated route resolution also appears without operator
consent.

### Option B: Generate configuration once

Add a command that resolves aliases and writes `hosts`, `exclude_hosts`,
`host_groups`, and `topology` after a preview.

Pros: explicit, reviewable, and no recurring resolution cost.

Cons: the generated copy becomes stale whenever OpenSSH configuration changes;
operators must rerun and reconcile the import.

### Option C: Add an opt-in resolved-topology discovery policy

Keep alias-only discovery as the compatibility default for existing files. A
typed `ssh_discovery` policy selects topology resolution and sets bounded refresh
and per-alias resolution timeouts. The service caches one immutable discovery
snapshot shared by collection and the dashboard.

Pros: continuously follows OpenSSH, remains opt-in, avoids repeated web-request
resolution, and provides one source for inventory, grouping, and topology.

Cons: `ssh -G` remains environment-sensitive and adds bounded local subprocess
work. Opaque `ProxyCommand` forms cannot always expose a jump alias.

## Decision

Choose Option C. `ssh_discovery.mode` is either `aliases` or `topology`.
Configurations that omit the object retain alias-only behavior. Newly generated
configurations select topology mode with a bounded refresh interval and resolution
timeout.

Topology mode asks OpenSSH for the effective configuration of eligible aliases
using `ssh -G`; it never initiates an SSH connection. Only `proxyjump` and
`proxycommand` are consumed. `ProxyJump` chains are normalized to known safe
aliases. A common SSH-backed `ProxyCommand` may contribute an exact known alias;
opaque commands produce a synthetic non-sensitive infrastructure node rather than
publishing command text or network identities.

Aliases referenced as proxy hops are infrastructure. They are excluded from the
automatically admitted probe inventory, but an explicit active `hosts` entry still
overrides that inference. An automatically admitted target that cannot be resolved
is omitted. Explicit hosts remain active so discovery failure cannot silently
remove operator-authorized monitoring.

The closest resolved proxy alias becomes the inferred group. Direct targets that
share a numbered alias prefix fall back to that prefix only when at least two targets
match. Explicit `host_groups` override inferred values and may predeclare safe aliases
in auto-discovery mode without making them probe targets. A configured `topology`
overrides the generated display tree. Explicit `exclude_hosts` always wins. Resolution is cached
under the policy refresh interval, uses the existing bounded worker limit, and is
recomputed immediately after a relevant in-process configuration change.

The scheduler and dashboard inventory consume the same typed discovery snapshot.
Topology remains descriptive; it never supplies a destination or process argument
to the resource probe. The only inventory authority added by inference is removal
of infrastructure aliases from the automatic candidate set.

## Impact

- Jump aliases no longer require name-based exclusions in topology mode.
- OpenSSH route changes appear after the bounded refresh interval.
- Manual inventory, grouping, and topology continue to override inference.
- A dashboard topology request normally reads the shared cache; the first request
  may populate it when collection has not yet done so.
- `ssh -G` parses the operator-owned OpenSSH configuration and may evaluate the
  same `Match exec` policy that normal SSH use evaluates. The opt-in mode does not
  expand destination authority beyond aliases that alias discovery would otherwise
  admit or explicit hosts already authorize.
- ADR-0008 remains the contract for configured topology and is superseded only for
  installations that select resolved topology discovery.
