# ADR-0023: Non-destructive host migration

## Status

Accepted

## Context

Mocop can initialize an empty configuration and install a user service, but it has
no lifecycle entry point for moving an existing installation to another monitoring
machine. Copying the old configuration verbatim leaves the previous `local_host`,
absolute SSH paths, machine-specific maintenance and incident state, compatibility
discovery policy, and possibly a copied dashboard capability. `config check` proves
schema validity but cannot distinguish portable fleet policy from old-machine state.

The first real migration exposed these gaps as incorrect local naming, unavailable
automatic grouping, stale local identity in the dashboard, and fragile service
replacement. A repeatable migration must prevent those classes of failure without
silently broadening the SSH probe inventory or overwriting the only source config.

## Driving factors

- Never overwrite or mutate the source configuration.
- Produce a private, current-schema target that passes the normal config loader.
- Rebind an existing local target to the new machine without retaining old-machine
  expected GPU counts, overrides, maintenance, or incident actions.
- Preserve portable remote inventory, groups, thresholds, notifications, and other
  fleet policy.
- Treat automatic host admission as an explicit operator decision.
- Never copy the dashboard capability, webhook environment, systemd unit, SSH
  credentials, or SQLite state as an implicit side effect.
- Perform no SSH connection, service install, restart, or database migration.

## Candidates

### Option A: Document raw directory copying

Copy the configuration, token, environment, state, and unit, then repair paths and
identity manually.

Pros: no new code and preserves every artifact.

Cons: copies credentials, embeds the old interpreter and host identity, mixes binary
and database compatibility, and provides no deterministic validation of what is
portable.

### Option B: Rewrite the copied configuration in place

Add a command that edits an existing target file and keeps a backup.

Pros: convenient after an operator has already copied the installation directory.

Cons: backup/rollback semantics become part of the command, partial copies may already
contain a reusable capability, and source and target intent remain ambiguous.

### Option C: Generate a new configuration from a private source file

Add `mocop migrate --from-config SOURCE`. It transforms validated JSON into a new
exclusive `0600` target, defaults its SSH config path to `~/.ssh/config`, upgrades SSH
discovery metadata, and rebinds an existing local target to the current hostname or an
explicit alias. It reports dropped machine-bound fields and leaves installation to the
existing doctor/service workflow.

Pros: non-destructive, testable as a pure transformation plus a narrow filesystem
boundary, and incapable of silently copying adjacent credentials or state.

Cons: operators must deliberately migrate webhook environment and optional history,
and custom SSH paths require an explicit argument.

## Decision

Choose Option C. The migration command refuses an existing target, an unsafe source or
target, a target directory containing a pre-existing `access-token`, alias collisions,
and invalid transformed output. Source JSON remains the canonical interchange format;
the current strict loader validates both source and generated target.

When the source has `local_host`, migration replaces that alias with the current
hostname by default. `--local-host` overrides the new alias; `--drop-local-host`
removes local collection. The old local alias is removed from machine-bound expected
GPU counts, host overrides, maintenance, host incident overrides, and incident actions.
Its group follows the new alias. A configured topology renames the old local node when
rebinding; if local collection is dropped and the topology references that node, the
configured topology is removed rather than producing an invalid or misleading tree.
`--display-name` creates only the new local display override.

Migration writes `ssh_discovery.mode: topology` while retaining bounded refresh and
resolution timeouts. It preserves `auto_discover` unless the operator supplies
`--auto-discover` or `--no-auto-discover`; therefore migration cannot silently add SSH
probe targets. `ssh -G` is not run by the migration command. The generated config must
still pass `config check` and `doctor --no-connect` before `service install`.

## Impact

- Cross-machine setup has one reproducible, non-destructive entry point.
- A new installation receives a fresh capability from `service install`; copied tokens
  are rejected at the migration target boundary.
- SQLite history and webhook secrets remain explicit, separately governed operations.
- Existing `init`, `config check`, foreground runtime, and service installation contracts
  are unchanged.
- `migration.py` owns the transformation and exclusive private-file boundary;
  `__main__.py` only parses arguments and presents the sanitized report.
