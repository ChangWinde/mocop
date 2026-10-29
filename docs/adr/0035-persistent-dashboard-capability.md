# ADR-0035: Persistent dashboard capability storage

## Status

Accepted; supersedes ADR-0017 only for browser credential retention.

## Context

Operators reopening the same local monitor need its previously accepted
capability without copying the private token for every new browser session.

## Decision

Retain a successfully authenticated capability in origin-scoped localStorage.
Migrate an existing valid sessionStorage value, and fall back to sessionStorage
or in-memory authentication when persistent browser storage is unavailable.
Clear both stores on logout, rejection or an invalid capability fragment.
Fragments are scrubbed from the location before other application work.

## Consequences

A shared browser profile carries access across tabs and restarts. Operators
must log out or clear site data to remove that access. The token remains a
bearer credential, readable by scripts at the monitor origin; the existing
same-origin, Content Security Policy and HTTP authorization boundaries remain
required. No Cookie, URL query credential or backend token distribution is added.
