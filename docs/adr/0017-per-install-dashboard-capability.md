# ADR-0017: Per-install dashboard capability

## Status

Accepted

## Context

Mocop has no application accounts and normally listens on TCP loopback. Loopback
limits network reachability but is shared by every Unix user on the machine; it is
therefore not an authorization boundary on a multi-user host. Telemetry, process
identity, configuration writes, manual probes, webhook tests, and supervised restart
must not be available merely because another process can connect to `127.0.0.1`.

The solution must work in an ordinary browser and with command-line automation,
preserve the dependency-free runtime, avoid ambient credentials, and keep the public
liveness/readiness probes useful. It does not attempt to create separate viewer and
administrator roles.

## Driving factors

- Isolate the private API from unrelated local users and opportunistic processes.
- Keep credential delivery out of HTTP request targets and server logs.
- Make browser and automation behavior explicit and testable.
- Retain the same-origin, trusted-Host, bounded-JSON, and Fetch Metadata defenses.
- Avoid a mandatory reverse proxy or new runtime dependency for the default install.

## Candidates

### Option A: Loopback-only listener with no credential

- Pros: no token lifecycle and native `EventSource` works unchanged.
- Cons: every local user can read telemetry and construct protected-read/write
  headers; a loopback listener does not identify the caller. This is inadequate on a
  shared host.

### Option B: Ambient HTTP cookie

- Pros: browsers attach it automatically, including to native `EventSource`; common
  server-side session patterns apply.
- Cons: the cookie is ambient authority, creates CSRF and session-fixation concerns,
  needs a bootstrap/delivery mechanism anyway, and tends to persist beyond one page.
  It obscures which automation requests carry authority.

### Option C: Bearer capability delivered in a URL fragment and kept per tab

- Pros: URL fragments are not sent in HTTP requests; the dashboard can scrub the
  fragment immediately, retain the capability in tab-scoped session storage, and add an explicit
  `Authorization` header to every protected fetch. The same token works for bounded
  command-line automation. No cookie, account database, or new dependency is needed.
- Cons: closed tabs, bookmarks, and independent new tabs need the printed capability URL again;
  authenticated SSE must use fetch streaming because native `EventSource` cannot set
  the header. Script execution in the dashboard origin could read the in-memory token.
  Plain HTTP does not protect the header on a remote network, and a process that wins
  the local port before Mocop can impersonate the server and receive the fragment.

### Option D: Private AF_UNIX listener with a browser-facing proxy

- Pros: a `0600` Unix socket provides strong kernel-enforced local-user isolation and
  avoids putting the application secret on TCP.
- Cons: browsers cannot connect to AF_UNIX directly. A proxy becomes a required
  deployment component and must itself authenticate the browser, preserve streaming,
  constrain headers, and own TLS for remote use. This conflicts with a dependency-free
  default and moves rather than removes capability delivery.

## Decision

Choose Option C for the default installation. `mocop service install` creates a
cryptographically random `access-token` beside the selected configuration, validates
it as an owner-only regular non-symlink file, and passes its absolute path to the
service. A foreground launch creates an ephemeral token. Both commands print a
capability URL of the form `http://127.0.0.1:8787/#access_token=...`.

The fragment never enters an HTTP request. The dashboard validates it, calls
`history.replaceState` before normal operation, and keeps the token in tab-scoped
`sessionStorage` so reload and managed restart recovery work. It creates no cookie and
does not persist the token in `localStorage` or IndexedDB. Protected requests carry exactly one `Authorization: Bearer <capability>`
header. Automation reads the private token file and sends the same header. Authenticated
SSE uses a streaming fetch parser.

For bare or forwarded dashboard URLs, the same page may collect the capability in
a non-dismissible form. It validates the token against a protected snapshot before
retaining it in `sessionStorage`; rejection clears the candidate and stops automatic
retries. This changes only the bootstrap interaction, not the capability scope or
server-side authorization boundary.

### Browser implementation boundary

Three implementation boundaries were considered for the bootstrap interaction:

- Keeping capability lifecycle and prompt state inline in `app.js` minimizes files,
  but expands the main rendering orchestrator beyond its reviewed line ceiling and
  couples authentication tests to the full dashboard.
- Moving the dashboard to ES modules or a bundled framework improves lexical
  encapsulation, but changes the dependency-free packaging and loading model for one
  bounded concern.
- A dependency-free classic-script leaf can expose one frozen factory, retain no
  server state, and be tested without the dashboard DOM.

Choose the classic-script leaf. `dashboard-auth.js` owns capability validation,
fragment scrubbing, tab-scoped retention, and prompt state. `app.js` supplies the
protected snapshot authentication callback and continues to own fetch, SSE, and the
dashboard lifecycle. The leaf is loaded before `app.js`; it neither creates an
ambient credential nor introduces a second transport or API.

The route manifest defines four access tiers:

- **P / `public`:** API discovery plus liveness/readiness; no capability.
- **A / `authenticated`:** Bearer capability; no viewer-presence side effect.
- **R / `reader`:** A plus trusted `Host`, dashboard marker, and same-origin/none Fetch
  Metadata when present.
- **W / `writer`:** R plus trusted `Origin`, exact JSON media type/schema, and the
  route-specific body limit.

Host, Origin, Fetch Metadata, and marker checks remain browser confused-deputy
defenses; the Bearer capability is the authentication factor. The capability grants
one operator role, not per-user or multi-tenant authorization.

### Forwarded dashboard origins

Three policies were considered for same-origin previews whose proxy rewrites the
backend `Host` while preserving the browser's public `Origin`:

- Exact per-session host entries provide the narrowest authority, but ephemeral
  preview names require configuration changes and service restarts for every session.
- A bounded HTTPS origin suffix supports ephemeral names while preserving an exact
  backend Host check and mandatory Bearer authentication.
- Trusting arbitrary loopback proxies or `X-Forwarded-*` headers would let an
  unauthenticated client assert proxy identity and was rejected.

Choose the bounded suffix policy. An exact `trusted_web_hosts` entry remains valid for
both Host and Origin. A leading `*.` entry matches only HTTPS Origins strictly below
that DNS suffix: it does not match the apex, IP literals, plain HTTP, or suffix
confusion such as `preview.example.attacker.invalid`. It never expands the trusted
backend Host set. Single-label broad entries such as `*.com` are rejected;
operators must still choose the narrowest suffix controlled by their proxy.

## Impact

- A copied capability grants the whole operator API until the managed token is
  rotated; it must be protected like the configuration and webhook environment file.
- Reloading in the same tab retains access through tab-scoped `sessionStorage`.
  Closing the tab or opening an independent tab loses access; operators then reuse
  the capability URL printed by the foreground command or `mocop service install`.
- `/api/meta`, `/healthz`, and `/readyz` remain public; telemetry, SSE, `/metrics`, and
  every write require Bearer authentication.
- Remote exposure still requires authenticated TLS or a private VPN. Bearer over plain
  HTTP provides authorization but no network confidentiality or server authentication.
- Ephemeral preview deployments can authorize a narrow HTTPS Origin suffix without
  delegating Host validation or trusting forwarded headers; the proxy and every name
  below that suffix remain part of the deployment trust boundary.
- AF_UNIX plus a carefully configured authenticated proxy remains a future hardened
  deployment option, not a hidden guarantee of the generated user service.
