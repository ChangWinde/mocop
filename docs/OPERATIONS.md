# Operations, upgrade, and rollback

This runbook covers the generated user-level systemd service. Package
installation and service installation are separate: changing the installed
package does not restart the running process; `mocop service install` validates
the selected configuration, regenerates the unit, restarts it, and verifies
that it becomes active.

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
   requires the printed URL again.

Treat a successful systemd restart as necessary but insufficient: it does not prove
SSH reachability, readiness, history restoration, webhook delivery, or browser
rendering.

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

## Related references

- [Configuration fields and boundaries](CONFIGURATION.md)
- [HTTP API and Bearer examples](API.md)
- [Security model](SECURITY.md)
- [Capability decision](adr/0017-per-install-dashboard-capability.md)
