# ADR-0026: Dashboard self-update

## Status

Accepted

## Context

Upgrading Mocop is a manual two-command host workflow. Operators asked for a
dashboard control that shows whether the running release is current and, when
it is not, applies the upgrade in one click. Mocop's standing constraints make
this consequential: the runtime performs no outbound requests besides SSH to
configured hosts, the browser must never supply process arguments, ADR-0024
already rejected downloaded install scripts, and ADR-0012's supervised restart
deliberately signals a graceful exit instead of running `systemctl` or a shell.

## Driving factors

- Keep zero outbound network traffic as the shipped default.
- Never let a browser request choose code to execute; the browser may only
  trigger a fixed, server-owned action.
- Keep the supply chain pinned: one hardcoded official repository, immutable
  release artifacts, and hash verification.
- Never execute downloaded code during installation itself: wheels only,
  no sdist builds, no bootstrap scripts.
- Fail visible and fail safe: a failed installation must leave the running
  process serving and must not restart into a broken environment.
- Bound every input: response sizes, artifact sizes, version grammar, and
  polling cadence.

## Candidates

### Option A: Update notification only

Show a banner when a newer release exists and link to the manual commands.

Pros: no new execution path; the smallest possible attack surface.

Cons: does not deliver the requested one-click flow; the operator still logs
in to every monitor host.

### Option B: Server-owned wheel self-update behind an opt-in mode

A typed `updates` policy selects `off` (default), `check` (poll and display),
or `self-update` (also allow the one-click apply). The service polls the
hardcoded official GitHub repository, and apply downloads the release wheel
plus its SHA-256 manifest, verifies them, installs with the environment's own
installer, verifies the installed version, then triggers the existing
supervised restart.

Pros: one click end to end; the browser only sends an empty authenticated
same-origin POST; the target version is chosen server-side from the latest
release, so a request cannot select or downgrade a version; wheel-only
installation never executes downloaded code at install time.

Cons: `check` and `self-update` add Mocop's first non-SSH outbound requests
(GitHub API and release assets over HTTPS), and installation runs the local
`pip` or `uv` toolchain from the service account.

### Option C: Browser-triggered bootstrap script

Download and run a shell installer.

Pros: shortest implementation.

Cons: rejected for the same supply-chain and shell-execution reasons as
ADR-0024; additionally reachable from the web surface.

## Decision

Choose Option B. `updates.mode` defaults to `off`, so a stock installation
still makes no outbound requests. `check` polls
`https://api.github.com/repos/ChangWinde/mocop/releases/latest` on a bounded
interval (1-24 h, default 6 h) with bounded response sizes; the repository is
hardcoded and not configurable, so no configuration value can redirect the
supply chain. Release tags must match `v<major>.<minor>.<patch>` and only a
version strictly newer than the running one is ever offered.

`self-update` additionally enables `POST /api/update/apply` (writer tier,
empty body). Apply is single-flight, requires the supervised-restart
capability, re-reads the latest release, downloads `SHA256SUMS` and the
exact `mocop-<version>-py3-none-any.whl` asset into a private temporary
directory, verifies the digest, and installs the local wheel with the first
available fixed toolchain: `python -m pip install --no-deps --force-reinstall`
for pip-managed environments, otherwise `uv tool install --force --from
<wheel> mocop`. Installation success is then proven by asking the target
interpreter for the installed `mocop` version; only a verified match triggers
the ADR-0012 supervised restart. On any failure the update reports `failed`
with a manual-recovery hint, no restart is signalled, and the running process
keeps serving from memory.

`GET /api/update` (reader tier) exposes the mode, current and latest
versions, state, and failure detail. The dashboard renders one header pill:
current, update available, applying, or failed. The pill's apply action sends
the fixed empty POST and then reuses the existing restart-recovery flow that
waits for a new `startedAt` before reloading.

## Impact

- A stock configuration is unchanged: no outbound requests, no new routes
  advertised as usable, and the pill stays hidden.
- With `check`, operators see release currency in the dashboard without any
  execution surface; with `self-update`, upgrading a monitor host is one
  authenticated click.
- The browser cannot name a version, a repository, an artifact, or an
  installer flag; every apply installs the verified latest official wheel.
- A crash-looped or hijacked-port scenario cannot be worsened: apply runs in
  the service process itself, not in the installer, and transmits no
  capability anywhere.
- `updates.py` owns policy parsing, version comparison, polling, download
  verification, installation, and the state machine; `web.py` only routes;
  ADR-0012 remains the restart authority.
