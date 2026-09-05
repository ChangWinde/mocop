# Operations, upgrade, and rollback

This runbook covers the generated user-level systemd service. Installing a package
never changes a running service. A fresh host uses `mocop deploy`; an existing setup
uses `mocop service install` to validate the selected configuration, regenerate the
unit, restart it, and verify that it becomes active.

## Installed state and ownership

With default XDG paths, Mocop uses:

| Artifact | Default location | Removed by `service uninstall`? |
|---|---|---:|
| Configuration | `~/.config/mocop/config.json` | No |
| Bearer capability | `~/.config/mocop/access-token` | No |
| Optional webhook environment | `~/.config/mocop/environment` | No |
| Generated unit | `~/.config/systemd/user/mocop.service` | Yes |
| SQLite history | `~/.local/state/mocop/history.sqlite3` | No |
| Browser preferences/background | browser `localStorage` / `IndexedDB` | No |
| Logs | user journal | No |
| SSH config, keys, known hosts, agent, sockets | operator-managed OpenSSH paths | Never |
| Linger policy | login manager policy | Never |

`XDG_CONFIG_HOME` changes the default configuration root, while the generated
unit intentionally keeps one canonical `~/.config/systemd/user/mocop.service`
path per UID. `XDG_STATE_HOME` changes foreground state. A custom `--config`
places `access-token` and optional `environment` beside that file. Under the
generated service, SQLite resolves through systemd's `STATE_DIRECTORY`
(`StateDirectory=mocop`).

The configuration directory is owner-controlled; generated configuration/token
files use `0600`, and every accepted secret file (including an operator-created
`environment`) denies group/other access. The state directory is `0700`, and the
unit sets `UMask=0077`.
The user unit also sets `NoNewPrivileges=true` and restricts address families.
It does not claim a mount-namespace filesystem sandbox: user-manager support and
behavior for `PrivateTmp`, `ProtectSystem`, and `ReadWritePaths` vary by systemd
version and deployment, and those directives can break required SSH agent or
ControlMaster paths. Ordinary Unix ownership/modes remain the filesystem boundary.

## Routine health checks

```bash
mocop config check
mocop service status
curl --fail --silent http://127.0.0.1:8787/healthz
curl --fail --silent http://127.0.0.1:8787/readyz
journalctl --user -u mocop --since today
```

`/healthz` proves the process is serving. `/readyz` is `503` until there is at
least one target and one successful collection. Both are intentionally public.
Telemetry and `/metrics` require the Bearer capability:

```bash
MOCOP_TOKEN="$(<"${XDG_CONFIG_HOME:-$HOME/.config}/mocop/access-token")"
curl --fail --silent -H "Authorization: Bearer ${MOCOP_TOKEN}" \
  http://127.0.0.1:8787/api/snapshot | jq '.startedAt'
curl --fail --silent -H "Authorization: Bearer ${MOCOP_TOKEN}" \
  http://127.0.0.1:8787/metrics | head
```

Do not export the token globally, paste it into tickets, or enable shell tracing
around these commands. A command-line header may be briefly visible to same-user
process inspection; prefer a same-user, locked-down scraper configuration for
continuous automation.

## Backup before upgrade

SQLite migration and package rollback are separate from the installer's unit-file
rollback. A consistent pre-upgrade backup is therefore mandatory when persistence
is enabled.

1. Record the current package version/ref and the resolved paths shown by
   `mocop config check`.
2. Check current health, then stop the writer:

   ```bash
   systemctl --user stop mocop.service
   ```

3. Create an owner-only backup directory on a filesystem with enough space.
   Copy `config.json`, `access-token`, optional `environment`, and the entire
   Mocop state directory while the service is stopped. Preserve file modes.
4. Record the generated unit and `systemctl --user is-enabled mocop.service`
   state for diagnosis. The unit can be regenerated and is not a substitute for
   the config/state backup.
5. Keep the backup outside the live config/state directories. It contains a
   dashboard capability, webhook secrets when `environment` is present, fleet
   inventory, and process history; protect it like credentials.

The current SQLite `user_version` is 3. Startup migrations are atomic and preserve
the released v3 physical table shapes so its positional writer remains
rollback-compatible. Internal usage anchors use reserved rows in the existing
process-event table; older runtimes discard their impossible host alias, while both
versions retain and prune them with the same bounds. Startup removes companion
tables/triggers created by short-lived development builds. A future binary may raise
the schema version and an older binary may then reject it. Never test any downgrade
against the only copy of production state.

## Upgrade and verification

1. Install the desired package version using the same package manager and Python
   environment as the existing command. Record the immutable release tag, wheel,
   or commit used; do not rely only on a moving branch name.
2. Run `mocop config check` before changing the service.
3. Run `mocop service install`. Installation captures the prior unit and enabled/
   active state, regenerates the unit for the active interpreter and config,
   restarts it, and rolls that service state back if installation fails before
   verification.
4. Verify `service status`, public health/readiness, one authenticated snapshot,
   and the journal. Confirm `startedAt` changed and the expected host count is
   present. If persistence is enabled, open a history view and check the snapshot's
   `persistence.healthy` value.
5. Open the exact `Dashboard:` capability URL printed by the installer. The page
   immediately removes the fragment and keeps the token in tab-scoped
   `sessionStorage`. Reloads remain authenticated; a closed or independent tab
   requires the printed URL again. A bare or forwarded dashboard URL instead
   presents a token prompt; paste the contents of the sibling `access-token`
   file. Use only a trusted TLS-terminating proxy because it carries subsequent
   Bearer-authenticated API traffic.

Treat a successful systemd restart as necessary but insufficient: it does not prove
SSH reachability, readiness, history restoration, webhook delivery, or browser
rendering.

## Fresh-host fast deployment

After installing the release pinned in the [README quick start](../README.md#quick-start)
on a server with a user-level systemd manager and an operator-owned OpenSSH
configuration, deploy Mocop with one local command:

```bash
"$(uv tool dir --bin)/mocop" deploy --display-name monitor-0
```

The explicit executable path works even when the current shell has not reloaded uv's
tool path. The command creates the normal private config, adds the current hostname as
the local target, enables automatic alias admission and resolved topology discovery,
then installs and health-checks the user service. Use repeatable `--host SSH_ALIAS` for
targets absent from OpenSSH discovery, `--local-host ALIAS` to choose the internal local
identity, `--no-local` for a controller-only monitor, and `--no-auto-discover` for an
explicit-only inventory. `--display-name` labels the local target and therefore
requires one: it is rejected together with `--no-local`.

Fresh deployment refuses an existing config, sibling `access-token`, or sibling
`environment`; it never overwrites or silently adopts an old installation. Use
`mocop service install` when a valid config already exists, or `mocop migrate` only
when transforming another
installation. If service verification fails, the unit rollback runs and the new config
remains available for `mocop config check` and `mocop doctor`.

## Cross-machine migration

Do not copy the entire Mocop config/state directory or the generated systemd unit to
another machine. In particular, the sibling `access-token` is an installation
capability, the unit embeds an interpreter path, and `local_host`, maintenance,
incident actions, SSH paths, and optional SQLite history may belong to the old machine.

Copy only the old `config.json` into a temporary owner-only location on the new
machine, set its mode to `0600`, install the new Mocop package, and generate a new
configuration:

```bash
chmod 600 /private/import/config.json
mocop migrate --from-config /private/import/config.json \
  --display-name new-console --auto-discover
mocop config check
mocop doctor --no-connect
mocop service install
```

The target defaults to the normal user configuration path and must not exist. Use
`--config PATH` for a different new target. If the source monitored itself through
`local_host`, migration replaces that alias with this machine's hostname; use
`--local-host ALIAS` to choose another safe identity, or `--drop-local-host` when the
new monitor must not collect itself. `--display-name` affects presentation only.
`--ssh-config` defaults to the new machine's `~/.ssh/config`.

Migration preserves the source `auto_discover` setting unless `--auto-discover` or
`--no-auto-discover` is supplied. It upgrades route discovery metadata to bounded
topology mode but does not run `ssh -G` or connect. Old-local expected GPU counts,
host overrides, maintenance, host incident overrides, and incident actions are
dropped; its group and configured topology node follow the new local alias. The
command reports every dropped field and never changes the source.

No capability, webhook environment, SSH credential, unit, or SQLite history is
copied. A target directory containing `access-token` is rejected so `service install`
creates a fresh capability. Migrate webhook variables manually into a new private
`environment` file. Move SQLite history only as a separate, stopped-service,
schema-compatible backup/restore operation described above. Complete live `doctor`,
readiness, authenticated snapshot, journal, and browser checks after installation.

## Rollback

Rollback is a coordinated binary-and-data operation:

1. Stop the service before restoring SQLite.
2. Reinstall the exact previously recorded package artifact/ref.
3. Restore the matching pre-upgrade configuration, capability/environment files,
   and state directory with their original ownership and private modes. Do not start
   an old binary on a database first opened by a newer schema.
4. Run `mocop config check`, then `mocop service install` to regenerate the unit for
   the restored interpreter and paths.
5. Repeat the complete verification above and retain both backups until normal
   collection, history, and notifications have been observed.

If only unit installation failed, rely on the installer's reported transaction
rollback and inspect the journal before retrying. If it reports that rollback is
incomplete, stop and reconcile the unit, enabled state, and running process manually;
do not assume the old service is active.

## Capability rotation

Rotate after suspected disclosure or when transferring operator control:

1. Stop the service.
2. Move the current sibling `access-token` into a private incident backup.
3. Run `mocop service install`; it creates a new private token and prints a new
   capability URL.
4. Update same-user scrapers, verify authenticated requests, then securely dispose
   of the revoked backup according to local policy.

Every open dashboard and client using the previous capability will lose access.
There is no grace period or multiple-token rotation window.

## Uninstall and retained data

```bash
mocop service uninstall
```

This stops/disables the service, removes only the generated unit, and reloads the
user manager. It intentionally retains configuration, access token, environment
file, SQLite state, browser storage, journal entries, SSH material/control sockets,
the installed Python package, and the login linger policy. Review and back up those
items before any separate manual cleanup; clearing them is not part of uninstall and
may be irreversible.

## Command reference and exit codes

Most commands accept `--config PATH`; without it Mocop uses `MOCOP_CONFIG`, then
`~/.config/mocop/config.json`, then a development-only `./config/mocop.json`, then
the bundled empty configuration. `service status` and `service uninstall` operate
on the generated unit and reject `--config`. `mocop --help` and
`mocop <command> --help` describe every flag. `--once` and `--strict` apply only
to the default monitor command.

| Command | Purpose | Notable flags |
|---|---|---|
| `mocop` | Foreground monitor with an ephemeral capability printed once | `--once` (print one JSON snapshot and exit), `--strict` (with `--once`, fail unless every host is online), `--version` |
| `mocop deploy` | Create a private config and install the verified user service on a fresh host | `--host ALIAS` (repeatable), `--local-host ALIAS` / `--no-local`, `--display-name`, `--ssh-config`, `--auto-discover` / `--no-auto-discover`, `--json` |
| `mocop init` | Create a private config only, never overwriting one | `--host ALIAS` (repeatable), `--json` |
| `mocop migrate` | Generate a new private config from another installation's config | `--from-config PATH` (required), the same identity flags as `deploy`, `--drop-local-host`, `--json` |
| `mocop api PATH` | GET one public or authenticated route from the running service and write the body to stdout; reader/writer routes are refused (`DASHBOARD_ONLY`) | `--token-file PATH` (default: the access-token file beside the config), `--timeout SECONDS` |
| `mocop config check` | Validate the configuration without a web server or SSH | `--json` (one JSON document on stdout, also for a rejected configuration) |
| `mocop doctor` | Read-only SSH reachability and connection-reuse diagnosis | `--host ALIAS` (repeatable filter), `--no-connect`, `--probe` (one production collection per alias), `--profile` (latency breakdown), `--json` |
| `mocop service install` | Generate, enable, start, and verify the unit; print the capability URL | `--json` |
| `mocop service status` | `systemctl --user status` for the generated unit | `--json` (`{ok, active, unitPath}`; journal text stays on the text path) |
| `mocop service uninstall` | Stop and remove only the generated unit | `--json` |

Exit codes are stable for automation:

| Code | Meaning |
|---|---|
| `0` | Success; for `doctor`, every selected alias is usable; for `api`, a 2xx response |
| `1` | Runtime failure: `doctor` found at least one unusable alias, `--once --strict` found a host without an online sample, the listener could not bind, the collector stopped, `service install` could not verify the service, or `api` received a non-2xx status or could not connect |
| `2` | Configuration or usage error: invalid or unreadable configuration, a `doctor` flag conflict or unknown `--host`, a lifecycle refusal (existing files, invalid alias), a managed unit missing its generated arguments, or an `api` target that is malformed, dashboard-only, or has no readable capability |
| `75` | Supervised restart requested from the dashboard or self-update; systemd's restart policy starts the replacement |

Every `--json` command writes one `{ok, ...}` document to stdout, including
refusals (`ok: false` plus a stable `code`). `config check`, `doctor`, `init`,
`deploy`, `migrate`, and the three `service` actions all accept `--json`.
`mocop --once` writes a snapshot document. `mocop api` is always
machine-readable: stdout is the server's response body, and a non-zero exit
leaves the server's or the client's `{error, code}` envelope there
(`INVALID_TARGET`, `DASHBOARD_ONLY`, `TOKEN_UNAVAILABLE`, `CONNECTION_FAILED`,
or the configuration code). Text-mode diagnostics stay on stderr.

## Related references

- [Configuration fields and boundaries](CONFIGURATION.md)
- [HTTP API and Bearer examples](API.md)
- [Security model](SECURITY.md)
- [Capability decision](adr/0017-per-install-dashboard-capability.md)
