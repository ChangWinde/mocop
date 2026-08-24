# ADR-0024: Fresh-host fast deployment

## Status

Accepted

## Context

Mocop exposes safe configuration initialization and verified user-service installation,
but a new monitoring server requires operators to discover and sequence both commands.
The common fresh-host case already has enough local information to choose useful
defaults: the current hostname, `~/.ssh/config`, resolved topology discovery, and the
loopback web listener. Migration from another installation is a separate workflow and
must not be required to start a new deployment.

## Driving factors

- Reduce a package-installed fresh server to one Mocop command.
- Discover safe OpenSSH aliases and topology without requiring inventory JSON edits.
- Include the current server as a local target by default while allowing explicit opt-out.
- Preserve the non-overwrite configuration and private per-install capability boundaries.
- Reuse the verified user-service installation and rollback behavior.
- Keep package acquisition separate so Mocop never executes a remote installer script.

## Candidates

### Option A: Document a shell chain

Tell operators to run `init`, edit JSON, run `doctor`, and run `service install` in one
copied shell block.

Pros: no new code and every existing command remains independently visible.

Cons: the operator still owns intermediate defaults and failure sequencing; copied
shell varies between documents and does not provide one stable product entry point.

### Option B: Add a local `mocop deploy` orchestrator

Create a fresh private config with safe deployment defaults, then invoke the existing
service manager and its health/rollback checks.

Pros: one command after package installation, no new dependency or execution boundary,
and the existing config, capability, systemd, and health contracts remain authoritative.

Cons: package installation remains a preceding step; a failed service start retains the
new config for diagnosis and requires `service install` after correction.

### Option C: Publish a remote bootstrap shell script

Download a script that installs Python tooling, installs Mocop, writes configuration,
and starts the service.

Pros: a visually compact command from an otherwise empty server.

Cons: expands the supply-chain and shell-execution boundary, duplicates package-manager
behavior, and makes installation provenance and partial failure harder to inspect.

## Decision

Choose Option B. `mocop deploy` is strictly a fresh-config operation. It refuses an
existing target, sibling `access-token`, or sibling `environment`, creates a `0600`
configuration, includes the current hostname as `local_host` by default, enables
automatic alias admission and
resolved topology discovery, and starts the verified user service. Repeatable `--host`
arguments add explicit aliases; `--no-local` and `--no-auto-discover` are narrow opt-outs.
`--display-name` changes presentation only, and `--ssh-config` selects the local OpenSSH
configuration path.

The command does not run a downloaded script, modify SSH files, or overwrite existing
Mocop state. Service installation creates a fresh capability and uses its existing
unit/health rollback. If service verification fails, that rollback restores the previous
unit while the newly created config remains available for `config check` and `doctor`.
An operator with an existing configuration continues to use `mocop service install`;
cross-machine transformation continues to use `mocop migrate`.

## Impact

- Fresh-host deployment becomes package installation plus one Mocop command.
- Automatic SSH topology and grouping are the default for this entry point.
- `init`, `migrate`, and `service install` remain explicit lower-level workflows.
- `__main__.py` owns orchestration, while `lifecycle.py` remains the configuration and
  user-service boundary.
