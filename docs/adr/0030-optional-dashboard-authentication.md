# ADR-0030: Optional dashboard authentication

## Status

Accepted. Extends [ADR-0017](0017-per-install-dashboard-capability.md), which
continues to govern the default Bearer mode.

## Context

Operators using a private tunnel want bare dashboard URLs to work in new tabs
without entering or retaining a capability. Mocop currently requires a token
for every non-public API request. Caller authorization, browser-origin
validation, and route/body validation are separate boundaries.

## Candidates

| Option | Convenience | Boundary and operational cost |
|---|---|---|
| Persistent browser credential | Repeated visits work after one login | Still requires a token for new browsers and adds long-lived browser secrets |
| Token-injecting local proxy | Bare URL works behind the proxy | Adds another service and transfers access control to proxy reachability |
| Explicit application `none` mode | Bare URL works in browsers and CLI | Every reachable client gains operator access; no additional dependency |
| Remove authentication globally | Bare URL works everywhere | Removes isolation from existing multi-user deployments on upgrade |

## Decision

Add the restart-scoped local configuration `authentication: "bearer" | "none"`.
Omission selects `bearer`. Invalid values and a missing Bearer token fail closed.
The web API cannot change this field. Operators selecting `none` accept that
local users and forwarded clients can both read telemetry and use the existing
operator actions. Host and Origin checks do not authenticate those clients.

Keep route tiers stable and expose `authentication.mode` plus the effective
`write.authorization` in `/api/meta`. `web_auth.py` owns the request policy.
In `none`, all non-public routes, unknown API paths, and wrong-method fallbacks
require a trusted Host and non-cross-site Fetch Metadata. Reader markers and
writer Origin/JSON/body guards remain mandatory. No CORS permission is added.

The browser attempts a snapshot and prompts only on `AUTHENTICATION_REQUIRED`.
This supports bare URLs, existing fragment URLs, reloads, and SSE with the same
transport. The CLI skips credential reads in `none`. Startup and installation
neither create nor read tokens in that mode, and installation verifies the
mode advertised by the live service as well as its anonymous snapshot response.

## Impact

No dependency, remote command, account system, or persistent browser credential
is added. Existing tokens are retained to support switching back to `bearer`.
Listener and forwarding controls remain operator responsibilities; enabling
`none` does not change the bind address or trust any forwarded header. A local
configuration change and service reinstall restore Bearer mode.

Tests cover anonymous reads, SSE, writes, CLI, lifecycle, browser reloads,
default authentication denial, wrong Host/Origin, cross-site requests, invalid
mode values, absent token files, and startup-mode mismatch.
