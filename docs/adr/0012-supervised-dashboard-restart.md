# ADR-0012: Supervised dashboard restart

## Status

Accepted

## Context

An operator who upgrades Mocop needs a clear way to load the new process and browser
assets. The dashboard must not gain general command execution or assume that every
foreground development process is managed by systemd.

## Driving factors

- The HTTP process runs with the operator's SSH identity and must keep least privilege.
- A restart request must be explicit, same-origin, bounded, and unavailable by default.
- The browser must distinguish process recovery from an ordinary SSE reconnect and
  load the new static assets automatically.
- Active SSH children must not hold shutdown open until their probe timeout expires.

## Candidates

### Option A: Invoke `systemctl --user restart` from the HTTP handler

- Pros: direct and familiar to operators.
- Cons: adds a command-execution boundary to the web process, requires systemd access
  inside the request path, and can terminate the response before acknowledgement.

### Option B: Expose a general lifecycle API

- Pros: could later support stop, reload, upgrade, and logs.
- Cons: substantially expands scope and privilege for one infrequent operation even
  when the caller has the dashboard's operator capability.

### Option C: Exit only when explicitly supervised

- Pros: no shell or systemd command enters the HTTP process; systemd's existing
  `Restart=on-failure` remains the sole process supervisor; foreground mode fails
  closed; the browser can wait for a new process identity before reloading.
- Cons: restart support depends on the generated service unit and intentionally does
  not appear for arbitrary foreground launches.

## Decision

Choose Option C. The generated user service passes a hidden `--managed-service`
capability. Only that mode installs a fixed restart callback which sets an in-process
event. `POST /api/service/restart` accepts exactly an empty JSON object through the
existing Bearer-authenticated, bounded same-origin write guard, acknowledges with
`202`, and then requests a
graceful exit with status 75. systemd starts the replacement process under its existing
failure policy.

The browser places this rare action under **Settings → Service status**, requires a
confirmation, never retries the POST, waits for `startedAt` to change, and then reloads
the page. The resource probe exposes an optional cancellation protocol so active local
or SSH process groups are terminated during shutdown.

## Impact

- The web layer cannot select or construct a command.
- A manually launched server reports restart as unsupported instead of exiting.
- Upgrades replace already-open browser assets without a manual hard refresh.
- Configuration, browser-local preferences, and optional SQLite history are not reset.
- The expected interruption is bounded by child-process cancellation and systemd's
configured restart delay.

The per-install HTTP capability and the P/A/R/W access tiers are defined separately
by [ADR-0017](0017-per-install-dashboard-capability.md).
