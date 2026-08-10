# ADR-0004: Dashboard-managed SSH inventory

## Status

Accepted

## Context

Operators can already inspect live telemetry and change the running collection cadence from the dashboard, but adding or removing a compute node still requires editing JSON and restarting the service. The dashboard should list literal aliases from the operator's OpenSSH configuration, omit recognizable Git, GitHub, and GitLab aliases, and let an operator promote a discovered alias into the monitored inventory or remove an active target. Browser input must not become an arbitrary SSH destination, command, path, or general-purpose configuration editor.

## Driving factors

- Keep the explicit JSON host inventory authoritative and reviewable.
- Never accept a browser-supplied destination that was not discovered locally.
- Apply inventory changes without restarting the process or interrupting healthy hosts.
- Preserve the dependency-free runtime and bounded OpenSSH-config parser.
- Keep configuration writes private, atomic, and narrowly authorized by the user service sandbox.

## Candidates

### Option A: Accept arbitrary aliases from the browser and probe them on demand

Pros: supports hosts that are absent from OpenSSH configuration and gives immediate connectivity feedback.

Cons: turns HTTP input into an SSH destination, increases request-triggered remote work, needs rate limiting and a stronger authentication model, and weakens the explicit inventory boundary.

### Option B: Expose a full JSON configuration editor and reload the file periodically

Pros: every setting becomes remotely editable and external file edits eventually take effect.

Cons: exposes unrelated security and performance controls, makes validation and conflict handling harder, performs unnecessary filesystem reads, and gives the browser excessive authority.

### Option C: Promote locally discovered aliases through a single-purpose inventory controller

Pros: reuses the bounded parser, constrains writes to host membership, validates the complete resulting configuration, supports atomic persistence and immediate in-process replacement, and adds no remote command path.

Cons: candidates must already be literal OpenSSH aliases; custom code-host aliases that do not contain a delimited `git`, `github`, or `gitlab` token still require `exclude_hosts` policy.

## Decision

Choose Option C. `HostSource.aliases()` exposes only literal, validated OpenSSH aliases. Recognizable Git, GitHub, and GitLab aliases are filtered server-side from automatic discovery and dashboard candidates, while an already explicit configuration entry remains operator-authorized. A dashboard add is accepted only when the alias is in a fresh eligible scan. A removal mutates only inventory-related fields and also removes stale expected-count, host-override, maintenance, and host-group entries; removing the local alias clears `local_host`.

`host_groups` is a narrow extension of the same inventory authority: it maps one explicit alias to one bounded visible group name. The dashboard may set or clear only that value for an already explicit host. The service publishes the normalized group with each host snapshot, while the browser decides whether to sort and render group sections. Arbitrary tags, group-triggered collection policy, and browser-local shared metadata remain out of scope.

`ConfigInventory` owns the write boundary. It serializes mutations, reloads the current file for each operation, writes a same-directory private temporary file, validates that complete candidate with the normal strict configuration loader, atomically replaces the target, and then invokes a typed runtime-update callback. The web layer depends only on the `DashboardConfigController` protocol and exposes one exact action schema. The service unit grants write access only to the selected configuration directory under `ProtectSystem=strict`, because atomic rename requires directory-level permission.

## Impact

- Scanning reads OpenSSH configuration files and includes but does not initiate an SSH connection.
- A new target enters the normal bounded scheduler immediately after persistence.
- A removed target disappears from in-memory state immediately and wakes the scheduler; stale in-flight results are ignored. If automatic discovery is enabled it is added to `exclude_hosts` so it stays removed.
- Invalid, duplicated, oversized, undiscovered, code-host, bundled-config, and partial-write paths fail closed.
- Group changes publish immediately, start no probe, and remain shared across browsers and process restarts.
- Presentation themes remain browser-local. Inventory membership is the durable surface defined by this decision; [ADR-0005](0005-dashboard-persisted-collector-settings.md) separately adds a typed collector-policy projection without exposing a general editor.
