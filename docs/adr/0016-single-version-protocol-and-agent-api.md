# ADR-0016: Single-version collection protocol and agent-facing API conventions

## Status

Accepted

## Context

The fixed remote collection script is not a deployed artifact: it lives in the
same Python package as its parser and is re-sent over stdin on every probe.
There is therefore no fleet of old emitters to stay compatible with — the only
producer of a payload is the exact process that parses it. Despite that, the
parser still accepted the three previous protocol versions (V4 through V6),
kept per-version field-count branches, and its test fixtures had to cover
every historical shape ([ADR-0014](0014-tiered-gpu-process-telemetry.md) and
[ADR-0015](0015-attended-cadence-and-identity-tier.md) each promised such
compatibility).

At the same time, automation started consuming the HTTP API. Agents need what
browsers do not: error responses they can branch on without parsing English
prose, a way to discover what a deployment supports before calling it, an
explicit deprecation signal, and honest documentation of side effects — in
particular that the dashboard marker header doubles as a viewer-presence
signal that changes collection cadence.

## Driving factors

- Keep the payload parser small and honest: every accepted branch must be
  producible by the current script, so tests prove real behavior instead of
  preserved dead code.
- Let protocol evolution stay cheap: one version constant, one parser, one
  fixture set per change.
- Make the HTTP API consumable by non-human clients without guessing:
  machine-readable failures, discoverable capabilities, predictable
  deprecation.
- Never let an always-on automation client silently defeat the unattended
  process-cadence savings.

## Decision

Two related policies:

- **Single-version collection protocol.** The parser accepts exactly the
  current protocol version (`MONITOR_V8`)
  and rejects everything else. Because the script and parser ship in one
  process and the script is re-sent on every probe, an older emitter cannot
  exist; read compatibility for the V4 through V6 payloads is removed rather
  than frozen. Every future protocol change bumps the version string and
  updates the parser and its fixtures in the same change — there is no
  multi-version transition window and no cross-version fixture matrix.
- **Agent-facing API conventions.** Every API error response carries a
  stable machine-readable `code` beside the human-readable `error`; unknown
  API-family paths and wrong methods answer in the same JSON envelope. A
  self-describing `GET /api/meta` endpoint reports the API/schema versions,
  capability flags, and the complete endpoint manifest (`API_ROUTES`, the
  single source of truth that `docs/API.md` is tested against). Endpoints are
  deprecated by answering with a `Deprecation: true` header and a CHANGELOG
  entry before removal. The viewer side effect of the
  `X-Monitor-Request: dashboard` marker — attended cadence for 30 seconds per
  marked read or stream wake — is documented as a contract: viewer clients
  send it, non-viewer automation must not. The route manifest uses the P/A/R/W
  tiers defined by [ADR-0017](0017-per-install-dashboard-capability.md): public,
  Bearer-authenticated, authenticated dashboard reader, and authenticated
  same-origin writer.

## Impact

- The parser and its tests cover exactly one payload shape; V4–V6 branches
  and fixtures are deleted, and a malformed or replayed old payload is now an
  explicit protocol error instead of a silently exercised legacy path.
- A Mocop upgrade atomically replaces script and parser, so mixed-version
  parsing can never occur; downgraded processes likewise only ever see their
  own version.
- Agents can branch on `code`, discover capabilities through `GET /api/meta`
  before acting, and detect deprecations mechanically from response headers.
- Automation that follows the marker contract keeps unattended fleets on the
  stretched process cadence; the A-tier read surface is sufficient for
  diagnosis without ever sending the marker.
- Legacy probe registry names are gone; `openssh-linux-v6` remains the single
  registered collector implementation name (it names the probe, not the
  payload protocol).
